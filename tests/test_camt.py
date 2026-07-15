import tempfile
import unittest
from pathlib import Path

from app.camt import parse_camt


CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.052.001.08">
  <BkToCstmrAcctRpt><Rpt><Id>REPORT-1</Id>
    <Acct><Id><IBAN>DE02120300000000202051</IBAN></Id></Acct>
    <Ntry><Amt Ccy="EUR">42.50</Amt><CdtDbtInd>CRDT</CdtDbtInd>
      <BookgDt><Dt>2026-07-14</Dt></BookgDt><ValDt><Dt>2026-07-14</Dt></ValDt>
      <AcctSvcrRef>REF-001</AcctSvcrRef>
      <NtryDtls><TxDtls><Refs><EndToEndId>E2E-001</EndToEndId></Refs>
        <RltdPties><Dbtr><Nm>Max Mustermann</Nm></Dbtr><DbtrAcct><Id><IBAN>DE11111111111111111111</IBAN></Id></DbtrAcct></RltdPties>
        <RmtInf><Ustrd>Mitgliedsbeitrag 2026</Ustrd></RmtInf>
      </TxDtls></NtryDtls>
    </Ntry>
    <Ntry><Amt Ccy="EUR">19.99</Amt><CdtDbtInd>DBIT</CdtDbtInd>
      <BookgDt><Dt>2026-07-15</Dt></BookgDt><ValDt><Dt>2026-07-15</Dt></ValDt>
      <AcctSvcrRef>REF-002</AcctSvcrRef>
      <NtryDtls><TxDtls><RltdPties><Cdtr><Nm>Bürobedarf GmbH</Nm></Cdtr></RltdPties>
        <RmtInf><Ustrd>Rechnung 4711</Ustrd></RmtInf>
      </TxDtls></NtryDtls>
    </Ntry>
  </Rpt></BkToCstmrAcctRpt>
</Document>"""


CAMT_NESTED_PARTIES = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.052.001.08">
  <BkToCstmrAcctRpt><Rpt><Id>SPARDA-REPORT</Id>
    <Acct><Id><IBAN>DE02120300000000202051</IBAN></Id></Acct>
    <Ntry><Amt Ccy="EUR">25.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>
      <BookgDt><Dt>2026-06-01</Dt></BookgDt><ValDt><Dt>2026-06-01</Dt></ValDt>
      <AcctSvcrRef>SPARDA-001</AcctSvcrRef>
      <NtryDtls><TxDtls><Refs><EndToEndId>E2E-SPARDA-001</EndToEndId></Refs>
        <RltdPties>
          <Dbtr><Pty><Nm>Erika Beispiel</Nm></Pty></Dbtr>
          <DbtrAcct><Id><IBAN>DE11111111111111111111</IBAN></Id></DbtrAcct>
          <Cdtr><Pty><Nm>Beispielverein</Nm></Pty></Cdtr>
          <CdtrAcct><Id><IBAN>DE02120300000000202051</IBAN></Id></CdtrAcct>
        </RltdPties>
        <RmtInf><Ustrd>Mitgliedsbeitrag</Ustrd></RmtInf>
      </TxDtls></NtryDtls>
    </Ntry>
    <Ntry><Amt Ccy="EUR">19.95</Amt><CdtDbtInd>DBIT</CdtDbtInd>
      <BookgDt><Dt>2026-06-02</Dt></BookgDt><ValDt><Dt>2026-06-02</Dt></ValDt>
      <AcctSvcrRef>SPARDA-002</AcctSvcrRef>
      <NtryDtls><TxDtls><Refs><EndToEndId>E2E-SPARDA-002</EndToEndId></Refs>
        <RltdPties>
          <Dbtr><Pty><Nm>Beispielverein</Nm></Pty></Dbtr>
          <DbtrAcct><Id><IBAN>DE02120300000000202051</IBAN></Id></DbtrAcct>
          <Cdtr><Pty><Nm>Lieferant Beispiel</Nm></Pty></Cdtr>
          <CdtrAcct><Id><IBAN>DE22222222222222222222</IBAN></Id></CdtrAcct>
        </RltdPties>
        <RmtInf><Ustrd>Rechnung 2026-1</Ustrd></RmtInf>
      </TxDtls></NtryDtls>
    </Ntry>
  </Rpt></BkToCstmrAcctRpt>
</Document>"""


class CamtParserTest(unittest.TestCase):
    def test_parses_credit_and_debit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "statement.xml"
            path.write_text(CAMT, encoding="utf-8")
            report = parse_camt(path)

        self.assertEqual(report.account_iban, "DE02120300000000202051")
        self.assertEqual(len(report.transactions), 2)
        self.assertEqual(report.transactions[0].amount_cents, 4250)
        self.assertEqual(report.transactions[0].counterparty, "Max Mustermann")
        self.assertEqual(report.transactions[0].purpose, "Mitgliedsbeitrag 2026")
        self.assertEqual(report.transactions[1].amount_cents, -1999)
        self.assertEqual(report.transactions[1].counterparty, "Bürobedarf GmbH")

    def test_fingerprint_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "statement.xml"
            path.write_text(CAMT, encoding="utf-8")
            first = parse_camt(path)
            second = parse_camt(path)
        self.assertEqual(first.transactions[0].fingerprint, second.transactions[0].fingerprint)

    def test_parses_nested_party_choices_used_by_sparda(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sparda.xml"
            path.write_text(CAMT_NESTED_PARTIES, encoding="utf-8")
            report = parse_camt(path)

        self.assertEqual(report.transactions[0].counterparty, "Erika Beispiel")
        self.assertEqual(report.transactions[0].counterparty_iban, "DE11111111111111111111")
        self.assertEqual(report.transactions[1].counterparty, "Lieferant Beispiel")
        self.assertEqual(report.transactions[1].counterparty_iban, "DE22222222222222222222")

    def test_rejects_non_camt_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "other.xml"
            path.write_text("<root />", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Keine CAMT"):
                parse_camt(path)


if __name__ == "__main__":
    unittest.main()
