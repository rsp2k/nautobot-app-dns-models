# DNS Registration Model

The DNS registration model tracks registrar-zone assignments and registration lifecycle attributes.

- `dns_registrar` (DNS Registrar, mandatory): Registrar used.
- `dns_zone` (DNS Zone, mandatory): Zone that is registered.
- `status` (Status, mandatory): Registration status.
- `expiration_date` (date, optional): Domain expiration date.
- `auto_renewal` (boolean): Whether auto renewal is enabled.
- `registry_locked` (boolean): Whether registry lock is enabled.
- `transfer_locked` (boolean): Whether transfer lock is enabled.
- `privacy_enabled` (boolean): Whether privacy protection is enabled.
- `website_forwarding_enabled` (boolean): Whether website forwarding is enabled.
- `renewal_term_months` (integer, optional): Renewal term in months.
- `dnssec_enabled` (boolean): Whether DNSSEC is enabled.

Uniqueness is enforced for registrar and zone together, and each zone can only have one registration.

+++ 2.1.2 "Bitemporal records"

    On PostgreSQL deployments, `DNSRegistration` carries `valid_during`, `recorded_during`, and `entry_id` columns. Changes to registrar-side fields (lock flags, expiration date, DNSSEC status, etc.) close the prior belief window and open a new one — useful for tracking what the registrar told you over time, especially when late-arriving WHOIS data needs to be reconciled with what was already in Nautobot. Uniqueness on `(dns_registrar, dns_zone)` applies to the *current belief slice only*.

    See [Bitemporal Records](../user/feature_bitemporal.md) for the full model, query API, and migration notes. MySQL deployments retain the previous schema and behavior.
