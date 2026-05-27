# DNS Record Model

+++ 1.2.0 "DNS name length rules"

    When DNS validation is enabled (via the `DNS_VALIDATION_LEVEL` configuration), `DNSRecord` enforces the following DNS label and name length rules, as specified by [RFC 1035 §3.1](https://datatracker.ietf.org/doc/html/rfc1035#section-3.1):

    - Each label (the parts of the name separated by dots) must be no more than 63 bytes in wire format
    - Empty labels (e.g., consecutive dots or leading/trailing dots) are not allowed
    - The total length of the fully qualified DNS name (including the zone and all dots, in wire format) must not exceed 255 bytes

    See the [installation guide](../admin/install.md#app-configuration) for configuration options.

+++ 2.1.2 "Bitemporal records"

    On PostgreSQL deployments, the `DNSRecord` abstract base (and every concrete record subclass — A, AAAA, CNAME, MX, NS, PTR, SRV, TXT) carries `valid_during`, `recorded_during`, and `entry_id` columns. Calling `save()` after mutating a tracked field rotates the belief log instead of editing in place. The default manager returns the *current* belief only; `Model.all_versions.as_of(<dt>)` returns the point-in-time view.

    See [Bitemporal Records](../user/feature_bitemporal.md) for the full model, query API, and migration notes. MySQL deployments retain the previous schema and behavior.