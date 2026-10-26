---
name: s3-cost-review
description: Read-only review of an AWS account's Amazon S3 costs. Use when asked to reduce an S3 bill, find S3 waste, audit bucket lifecycle rules, or explain why S3 storage costs what it does. Checks every bucket for old versions kept forever, unfinished multipart uploads, and cold data paying the Standard rate, reads three months of the S3 bill by usage type, and proposes lifecycle rules without applying them.
---

# S3 cost review

Most S3 bills for business files are high for three boring reasons, and each one is a bucket setting:

1. Versioning is on and old versions are never expired.
2. Uploads that failed halfway still hold their parts.
3. Files nobody opens pay the S3 Standard rate.

This skill finds all three across every bucket, shows the bill by usage type so the user can see which line the money is in, and prints the lifecycle rule that would fix each finding. It never changes anything.

## Run it

Requires the AWS CLI with credentials that can read bucket settings, CloudWatch and Cost Explorer (policy below). Python 3.9 or newer, no packages.

```bash
python3 scripts/review.py --profile NAME > s3-cost-review.md
```

Options: `--buckets a,b` to review named buckets only, `--threshold-gb 10` to change the Standard size below which tiering is not suggested. `python3 scripts/review.py test` runs the self-check without touching AWS.

Give the user the report as written. The bucket table and the bill table are the facts; the findings are what to do about them.

## Read the findings

- **Old versions are kept forever.** Propose `NoncurrentVersionExpiration` with `NewerNoncurrentVersions: 1` so the most recent old version of every file survives. Ask the user what retention their business needs before suggesting a number of days; 90 is a starting point, not an answer. CloudWatch cannot size noncurrent bytes, so if the user wants the figure, enable an S3 Inventory report with the version column and read it a day later.
- **Unfinished uploads are never cleaned up.** `AbortIncompleteMultipartUpload` after 7 days is safe for any bucket: a finished upload is a normal object and is not affected. This is the one rule you can recommend without a follow-up question.
- **Everything pays the Standard rate.** The saving shown is a ceiling that assumes every byte is cold. Objects under 128 KB are excluded already. Before recommending the transition, ask how the files are used: a bucket that an application reads constantly gains nothing, and a Glacier-style archive tier is a different decision with retrieval delays. Intelligent-Tiering with only the Frequent, Infrequent and Archive Instant tiers has no retrieval fee and no delay.

## Rules

- Never apply a rule, delete an object or version, or change a bucket. Show the JSON, explain it, and let the user put it on the bucket. If they ask you to apply it, read the bucket's existing lifecycle rules first and send the full list back with the new rule added; `put-bucket-lifecycle-configuration` replaces all rules.
- Never estimate savings the report did not compute. If the bill table is missing, say Cost Explorer was not readable and stop there; do not guess at the bill from storage size.
- Prices in the script are us-east-1 list prices from October 2026. Say so when quoting them, and point the user at the bill table for their real numbers.
- If a bucket is skipped (printed on stderr), say which one and why; it is usually a permission the role does not have in that Region.

## IAM policy

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "s3:ListAllMyBuckets", "s3:GetBucketLocation", "s3:GetBucketVersioning",
      "s3:GetLifecycleConfiguration", "s3:ListBucketMultipartUploads",
      "cloudwatch:ListMetrics", "cloudwatch:GetMetricStatistics",
      "ce:GetCostAndUsage", "sts:GetCallerIdentity"
    ],
    "Resource": "*"
  }]
}
```
