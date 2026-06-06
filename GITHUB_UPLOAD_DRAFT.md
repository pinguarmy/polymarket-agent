# GitHub upload draft

Recommended flow: create a fresh private repository from this sanitized copy, inspect it on GitHub, then switch visibility to public only after final approval.

Do not upload the original private working tree or its existing git history.

## Proposed repo

- Owner: pinguarmy
- Name: polymarket-agent
- Initial visibility: private
- Source directory: this sanitized public copy

## Commands to run after approval

```bash
cd <sanitized-public-copy>

git status --short
git add .
git commit -m "chore: prepare sanitized public snapshot"

gh repo create pinguarmy/polymarket-agent --private --source . --remote origin --push
```

After checking the GitHub file browser and repository settings:

```bash
gh repo edit pinguarmy/polymarket-agent --visibility public
```

## If the repo name already exists

Use a staging name first:

```bash
gh repo create pinguarmy/polymarket-agent-public --private --source . --remote origin --push
```

Then rename it in GitHub settings or with:

```bash
gh repo rename polymarket-agent --repo pinguarmy/polymarket-agent-public
```

## Final pre-public checklist

- [ ] `git status --short --ignored` shows no tracked `.env`, `data/`, `logs/`, `*.csv`, `*.db`, or cache files
- [ ] Secret scan reports no private keys, API secrets, passphrases, hardcoded funder addresses, or personal absolute paths
- [ ] Tests pass or known data-dependent failures are documented
- [ ] User chooses a license or accepts public-but-all-rights-reserved
- [ ] User explicitly approves the `gh repo create` and later `gh repo edit --visibility public` steps
