# s3-cost-review

An agent skill that reviews an AWS account's Amazon S3 costs without changing anything. Works in Claude Code, Codex, Cursor, Kiro and any agent that reads `SKILL.md` files.

It checks every bucket for the three settings behind most S3 overspend on business files:

| Finding | What it means | Proposed fix |
|---|---|---|
| Old versions are kept forever | Versioning on, no `NoncurrentVersionExpiration` | Expire old versions after N days, keep the newest one |
| Unfinished uploads are never cleaned up | No `AbortIncompleteMultipartUpload` rule, stale uploads counted | Abort after 7 days |
| Everything pays the Standard rate | Large Standard footprint, no lifecycle transition | Intelligent-Tiering for objects over 128 KB, with the saving shown as a ceiling |

It also prints three months of the S3 bill by usage type from Cost Explorer, so the reader sees which line the money is in before touching a bucket.

## Install

```bash
npx skills add da3ferg/s3-cost-review-skill
```

Or copy `skills/s3-cost-review` into your agent's skills folder: `~/.claude/skills/`, `~/.codex/skills/`, `~/.cursor/skills/` or `~/.kiro/skills/`.

## Use

Ask the agent to review your S3 costs. It runs `scripts/review.py` through the AWS CLI with your current profile and hands you a Markdown report. Every AWS call is a Get or List; the IAM policy it needs is in `SKILL.md`.

You can also run the script yourself:

```bash
python3 skills/s3-cost-review/scripts/review.py --profile my-profile > s3-cost-review.md
```

## What it does not do

It does not apply any rule, delete anything, or size old versions (that needs S3 Inventory) or measure how often files are read (that needs Storage Lens or access logs). The tiering saving it prints is the most you could save if every file turned out to be cold.

If you want those measured and the fixes applied with retention rules you set, the [BucketDesk S3 Savings tool](https://app.bucketdesk.com/tools) does that inside your own account, free until verified savings pass $500.

## License

MIT
