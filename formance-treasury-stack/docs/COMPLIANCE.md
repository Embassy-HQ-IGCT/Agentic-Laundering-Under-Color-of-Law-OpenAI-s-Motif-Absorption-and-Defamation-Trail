# What this software is - and is not

This is **software**. It is not a bank, payment institution, money transmitter, card issuer, payment processor or clearing house, and it does
not hold customer funds. It records balances in a ledger and instructs *your* licensed partners (banks, issuer-processors, FX providers).

- Whoever **operates** it is responsible for holding every licence and registration their activity requires in each jurisdiction
  (e.g. banking / money-transmission / e-money / payment-institution licences, card-programme and network rules), for KYC/AML/sanctions
  screening and monitoring, for consumer-protection, privacy and record-retention duties, and for safeguarding customer funds.
- The engine intentionally has **no** sanctions screening, AML monitoring, fraud scoring or KYC; do not operate it for third parties without them.
- It never handles card numbers (PANs); keep it that way unless you accept PCI DSS scope.
- Nothing here is legal, regulatory or financial advice. Cash movement semantics (rail hours, limits, cutoffs, finality) in `rails.py` are
  defaults to be confirmed with your banks and operators.
- The software is provided "AS IS" without warranty (Apache-2.0, sections 7-8). Test with simulated banks first; moving real money is at your risk.
