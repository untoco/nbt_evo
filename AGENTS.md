# NBT Evo project

- This is the canonical workspace for NBT Evo tooling, native applications, HU research and associated coding/update studies. The private clubsport repository retains the real-car dossier.
- Use Russian for explanations and research notes; use English resource and file names.
- Keep all research, hypotheses, plans, comparisons, third-party project analysis, exploratory scripts/configuration and unreviewed logs under the local ignored research/ directory. Maintain one living plan per direction and a separate knowledge base there; extend them instead of creating parallel articles. Working document names have no date; dated evidence snapshots are allowed.
- Git contains only verified facts, reviewed anonymized evidence, working code/tests, reproducible environment recipes and operational instructions. Publish distilled verified findings in docs/facts.md with date, source and scope; publish selected measured reports in docs/evidence/. Never promote a whole mixed research document just because part of it is confirmed.
- Do not force-add research/ or link public documentation to local-only material. A fresh clone must have all files needed to read the public documentation; clearly identify external/local inputs required to run tools. Historical research already committed is not removed from old Git history by this policy.
- Distinguish measured, confirmed, owner-reported, source-reported, planned and unknown. Successful compilation does not prove HU execution, ABI compatibility or physical wiring.
- The owner authorizes connections and packets/diagnostic queries to the owned vehicle network. This does not imply blanket permission for flashing, persistent configuration changes, resets or new external disclosure. For changes preserve backup, one change, readback, functional verification and rollback.
- Keep private inputs, raw captures, credentials, actual VIN/MAC, SDK/licenses and HU binaries out of Git. Published log copies must explicitly identify anonymization. Never import private repository history.
- External diagnostic relay use requires existing session authorization covering the data and recipient. Feature Installer is excluded from this research.
- Python on macOS handles network access; Docker handles compilers and reverse engineering. Keep dependency versions and hashes pinned. Tests must not require a live car.
- Commit and push completed scoped work to main using existing GitHub authentication. Do not include unrelated changes. This repository is public: inspect staged changes before pushing.
