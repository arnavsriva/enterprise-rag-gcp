# ADR-0002: Private networking without NAT or VPC connectors

- **Status:** Accepted
- **Date:** 2026-10-02

## Context
Client security teams typically require that databases and vector indexes have no public endpoint.
The project also has a hard cost ceiling (~$120), so always-on network appliances must be avoided.

## Options considered
**Vector Search access:**
1. **Public endpoint with IAM:** simplest, no network cost, but publicly routable.
2. **Private Service Connect endpoint (chosen):** reachable only from inside the VPC via a
   forwarding rule.

**Cloud Run → VPC:**
1. **Serverless VPC Access connector:** at least 2 always-on instances, roughly $12+/month.
2. **Direct VPC egress (chosen):** no extra resources and no idle cost.

**Outbound internet (SEC EDGAR):**
1. **Cloud NAT:** roughly $32/month per gateway, plus data processing.
2. **`PRIVATE_RANGES_ONLY` egress (chosen):** only RFC 1918 traffic goes through the VPC, and
   internet and Google API traffic uses Cloud Run's default path.

## Decision
- **Network:** a custom VPC with one private /24 subnet (Private Google Access on).
- **Cloud SQL:** private IP only, via Private Service Access peering, with `ENCRYPTED_ONLY` SSL.
- **Vector Search:** a PSC-enabled index endpoint. When the index is deployed, an internal IP and
  a forwarding rule target the deployed index's service attachment.
- **Workloads:** Cloud Run (API) and Cloud Run Jobs (ingest, bench) use Direct VPC egress with
  `PRIVATE_RANGES_ONLY`.
- **API access:** the API stays reachable over HTTPS, but every call needs an IAM identity token
  (no `allUsers`).

## Consequences
- The Vector Search backend cannot be queried from a laptop. Local development uses pgvector, and
  anything that touches Vector Search (ingestion, benchmark) runs as a Cloud Run Job inside the VPC.
- The PSC forwarding rule adds a small hourly charge, but only while the index is deployed.
- The Private Service Access peering is `ABANDON`ed on destroy (deleting it races with Cloud SQL's
  teardown). It is removed together with the VPC.
- **Production hardening not done here:** VPC Service Controls perimeter, Cloud Armor/IAP in front
  of the API, CMEK, and a deny-all egress firewall with explicit allows.
