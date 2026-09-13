# Brand-constrained identity infrastructure sequencing

Use when a founder is simultaneously choosing an umbrella name, public GitHub identity, organization handle, custom-domain email, Workspace tenant, and automation Apps.

## Dependency order

```text
brand topology → naming territory → domain contract → internal candidate pool
→ pre-display domain feasibility → human finalist → deep collision/trademark checks
→ domain purchase → GitHub account/org → email tenant → bot Apps and brokers
```

Do not create permanent GitHub organizations, Workspace tenants, App names, or public OSS identities under a disposable company name. If account bootstrap is urgent, use a personal email temporarily; GitHub primary email can change later.

## Exact `.com` hard constraint

When the user requires exact `brand.com`, authoritative domain screening moves **before candidate presentation**:

1. Generate a broad internal pool.
2. Reject poor pronunciation, opaque fantasy phonetics, random suffixes, and narrow category terms.
3. Query Verisign RDAP for every survivor; do not show registered names merely because exact web search is quiet.
4. Apply `references/domain-constrained-brand-longlists.md` → Registration-evidence procedure. Do not turn RDAP/WHOIS/DNS into three mandatory independent proofs.
5. Then check exact web/company/game collisions, GitHub user/org/repositories, relevant package registries, and preliminary official trademark surfaces.
6. Describe the evidence as `no registration object found at check time`, never guaranteed purchasable or legally clear.

If a representative natural-English compound pool has near-zero exact `.com` availability, stop generating malformed variants. Ask which axis may move: semantically legible invented word, qualified `.com`, premium acquisition, or alternate TLD.

## Employer/public-identity boundary

A separate organization under the same public user separates permissions but not visible identity. If an employer monitors public repositories associated with that user and the founder needs a defensible independent studio/OSS identity, a separate personal/studio account plus a dedicated organization can be appropriate. This is governance separation, not anonymity or policy evasion:

- no employer email, SSO, organization membership, OAuth App, runner, or credential;
- no clone/token on employer-managed devices;
- separate commit attribution, passkeys/2FA, recovery, Apps, private keys, and vault records;
- employment/IP/open-source policies still apply.

For automation, human account security/billing/recovery remains human-owned. Persona Apps may own full repository lifecycle inside a homogeneous studio trust domain, while brokers hard-block permanent repository deletion/transfer, credential or secret management, billing, and security-control removal without explicit approval.
