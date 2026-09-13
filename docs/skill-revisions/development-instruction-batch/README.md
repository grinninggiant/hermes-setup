# Development instruction revision

Five existing skill bodies revised against the official Astra skill/prompt guide. Baselines are historical rollback fixtures, not active instructions. Candidates are the intended general-profile content. No automatic cross-profile rollout.

The review-mode change was explicitly approved for general only: self-review is labeled honestly; independent repository/security approvals remain required.

Run `SKILL_LIVE_ROOT=/absolute/profile/skills/software-development python3 test_revision.py` to check candidate/live parity, domain contracts and isolated rollback. These structural tests do not establish real model behavior or completion of the library-wide audit.

Source: https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra
