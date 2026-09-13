# Domain-Constrained Brand Longlists

Use this when a naming brief demands both a large candidate set and verified domain signals.

## Resolve the architecture first

Before generating names, establish whether the user needs one umbrella brand or independent brands for different businesses. Do not force gaming, open source, consulting, or future ventures into one name when they have distinct audiences and personalities.

## Lock the domain contract

Write the exact domain pattern before ideation:

- exact word `.com`
- category-qualified `.com` such as `<brand>games.com` or `<brand>code.com`
- prefixed `.com`
- alternative TLD

“Must be `.com`” does not necessarily mean the bare exact-word `.com`. Ask once and preserve the answer.

## Feasibility gate

Natural, familiar English + bare exact-word `.com` + 40–50 choices is usually an overconstrained brief. Test a representative batch with authoritative registry data before generating hundreds of names. If availability is near zero, present the trade-off immediately instead of deforming words with random suffixes.

Domain availability is a veto filter, not the naming engine. Reject pronounceability failures, opaque pseudo-words, fantasy/utopian tone, and names that need an etymology lecture even when their domains are open.

## High-volume workflow

1. Create separate semantic territories for each brand.
2. Generate a broad internal pool of understandable real words or natural phrases.
3. Apply tone, pronunciation, audience, and longevity filters before domain lookup.
4. Resolve the approved domain pattern for every survivor.
5. Apply the registration-evidence procedure below to each exact candidate.
6. Timestamp the check. Never call this ownership or guaranteed registrability: premium/reserved policies, race conditions, and trademark rights remain separate.
7. Present the requested longlist in compact tables with brand, plain-English meaning, and exact verified domain.
8. Have the user select a generous subset, then perform exact web/company, adjacent-sector, GitHub, package, social, and official trademark checks on that subset.

## Registration-evidence procedure

This is the canonical domain-evidence procedure for this skill; other references point here rather than defining competing gates.

- Preserve the exact domain and verify the registry endpoint for its TLD. For `.com`, query the registry RDAP endpoint `https://rdap.verisign.com/com/v1/domain/<domain>`.
- A successful response containing the exact domain registration object is evidence of registration. A genuine RDAP 404 means the queried server did not find the requested information, not that checkout will succeed. Validate endpoint, exact request and response origin; a proxy/WAF error page is not registry evidence.
- Timeout, 403, 429, malformed response or transport failure means **unknown**. Do not convert it to an available candidate. Respect rate limits and use bounded requests.
- DNS is a diagnostic signal, not a registration database: no A/AAAA/NS result does not establish non-registration. WHOIS can supplement a check where supported; do not require it as a universal second authority or treat RDAP/WHOIS from one registry as independent sources.
- Report exact domain, source, check time and limited conclusion: **no registration object found at check time**. Registrar availability/reservation/premium status and trademark/company/handle conflicts remain separate checks. Resolve contradictory evidence before recommending availability.
- Purchase, registration and account creation remain separately authorized actions. A read-only search does not authorize a cart or payment operation.

Protocol basis: [RFC 7480](https://www.rfc-editor.org/rfc/rfc7480), especially HTTP response semantics. No current availability claim may be made from this procedure alone.

## Quality rules

- A category qualifier can live in the domain without becoming the visible brand: `Spry` may use `sprygames.com`.
- Prefer an understandable brand with a qualified domain over an opaque invented word with a bare `.com`.
- Do not show malformed derivatives created only to evade registration.
- Keep gaming and OSS screens independent: delight/play/energy versus usefulness/openness/stewardship.
- Domain-free does not mean company-free, package-free, handle-free, or trademark-clear.
