# Software and OSS product-name preflight

Use this before showing software/tool names to the user.

## Sequence

1. Generate an internal pool sized to the requested deliverable; do not pad it to meet a fixed quota.
2. Apply pronunciation and direction filters first. If the user asked for poetic/simple names, eliminate mythological, Latin-heavy, hard-to-pronounce, or explanation-dependent candidates.
3. Map adjacent products and naming patterns in the category.
4. Run an exact-name web search for every surviving candidate. Eliminate active software, developer tools, adjacent products, companies, or confusingly similar brands before presenting a shortlist.
5. Query the relevant package registries directly (for example crates.io, npm, PyPI). HTTP 404 is only an availability signal at that instant.
6. Check GitHub organization/repository names and preliminary domain RDAP. DNS non-resolution is not ownership proof; RDAP 404 is still not trademark clearance.
7. Present the requested number of candidates that passed the lightweight scan, or explain a quality shortfall rather than padding. Disclose minor non-software collisions, pronunciation/autocorrect risk, package status, and domain uncertainty.
8. After the user selects a finalist, perform deeper GitHub, package, domain, social-handle, and official trademark-database checks. Registration or domain purchase requires the relevant approval/spending gate.

## Pitfalls

- Do not make the user reject names that a 30-second exact web search would have eliminated.
- Do not call a name “clean,” “available,” or “ownable” from DNS alone.
- Search engines may autocorrect coined names; treat autocorrect toward a common word as a discoverability risk.
- A name can be package-available but still conflict with an active company or adjacent product.
- For open-source tools, check whether the name works as project, CLI, daemon, provider key, and repository slug.
