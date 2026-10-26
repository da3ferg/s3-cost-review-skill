#!/usr/bin/env python3
"""Read-only Amazon S3 cost review.

Reads bucket settings, CloudWatch storage metrics and the S3 lines of the bill through the AWS CLI,
then prints a Markdown report of the three settings that cause most S3 overspend on business files:
old versions kept forever, abandoned multipart uploads, and cold data paying the Standard rate.

It never changes anything. Every AWS call is a Get, List or Describe.

    python3 review.py [--profile NAME] [--buckets a,b] [--threshold-gb 10]
    python3 review.py test        # self-check, no AWS calls
"""
import argparse
import datetime as dt
import json
import subprocess
import sys

# us-east-1 list prices, checked October 2026. Other Regions differ; the bill is the only number that matters.
PRICE_STANDARD_GB = 0.023
PRICE_IA_GB = 0.0125
PRICE_MONITORING_PER_1000 = 0.0025

STANDARD_TYPES = {"StandardStorage"}
UPLOAD_RULE = {"ID": "abort-unfinished-uploads", "Status": "Enabled", "Filter": {}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}


def aws(args, profile=None, region=None, ignore=()):
    cmd = ["aws", *args, "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    if region:
        cmd += ["--region", region]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        err = p.stderr.strip()
        if any(code in err for code in ignore):
            return None
        raise RuntimeError(f"{' '.join(cmd[:3])}: {err}")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def bucket_region(name, profile):
    loc = aws(["s3api", "get-bucket-location", "--bucket", name], profile)["LocationConstraint"]
    return {None: "us-east-1", "EU": "eu-west-1"}.get(loc, loc)


def storage_metrics(name, region, profile):
    """Average BucketSizeBytes per StorageType and NumberOfObjects over the last three days."""
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=3)
    window = ["--start-time", start.isoformat(), "--end-time", end.isoformat(), "--period", "86400", "--statistics", "Average"]
    listed = aws(["cloudwatch", "list-metrics", "--namespace", "AWS/S3", "--metric-name", "BucketSizeBytes",
                  "--dimensions", f"Name=BucketName,Value={name}"], profile, region)
    sizes = {}
    for m in listed.get("Metrics", []):
        stype = next(d["Value"] for d in m["Dimensions"] if d["Name"] == "StorageType")
        points = aws(["cloudwatch", "get-metric-statistics", "--namespace", "AWS/S3", "--metric-name", "BucketSizeBytes",
                      "--dimensions", f"Name=BucketName,Value={name}", f"Name=StorageType,Value={stype}", *window],
                     profile, region)["Datapoints"]
        if points:
            sizes[stype] = max(points, key=lambda p: p["Timestamp"])["Average"]
    points = aws(["cloudwatch", "get-metric-statistics", "--namespace", "AWS/S3", "--metric-name", "NumberOfObjects",
                  "--dimensions", f"Name=BucketName,Value={name}", "Name=StorageType,Value=AllStorageTypes", *window],
                 profile, region)["Datapoints"]
    objects = max(points, key=lambda p: p["Timestamp"])["Average"] if points else 0
    return sizes, objects


def inspect(name, profile):
    region = bucket_region(name, profile)
    versioning = aws(["s3api", "get-bucket-versioning", "--bucket", name], profile, region).get("Status", "Disabled")
    lifecycle = aws(["s3api", "get-bucket-lifecycle-configuration", "--bucket", name], profile, region,
                    ignore=("NoSuchLifecycleConfiguration",))
    uploads = aws(["s3api", "list-multipart-uploads", "--bucket", name, "--max-uploads", "1000"], profile, region,
                  ignore=("AccessDenied",))
    now = dt.datetime.now(dt.timezone.utc)
    stale = []
    for u in (uploads or {}).get("Uploads", []):
        age = (now - dt.datetime.fromisoformat(u["Initiated"].replace("Z", "+00:00"))).days
        if age >= 7:
            stale.append(age)
    sizes, objects = storage_metrics(name, region, profile)
    return {"name": name, "region": region, "versioning": versioning, "rules": (lifecycle or {}).get("Rules", []),
            "stale_uploads": stale, "uploads_readable": uploads is not None, "sizes": sizes, "objects": objects}


def gb(n):
    return n / 1024 ** 3


def assess(b, threshold_gb=10.0):
    """Pure: bucket facts in, findings out. Each finding is (key, title, detail, proposed lifecycle rule or None)."""
    enabled = [r for r in b["rules"] if r.get("Status") == "Enabled"]
    findings = []

    if b["versioning"] == "Enabled" and not any("NoncurrentVersionExpiration" in r for r in enabled):
        findings.append(("versions", "Old versions are kept forever",
                         "Versioning is on with no NoncurrentVersionExpiration rule, so every overwrite and delete adds a "
                         "stored copy that is never removed. CloudWatch does not split current from noncurrent bytes; "
                         "run an S3 Inventory report with the version column to size this.",
                         {"ID": "expire-old-versions", "Status": "Enabled", "Filter": {},
                          "NoncurrentVersionExpiration": {"NoncurrentDays": 90, "NewerNoncurrentVersions": 1}}))

    if not any("AbortIncompleteMultipartUpload" in r for r in enabled):
        stale = b["stale_uploads"]
        if stale:
            detail = ("No AbortIncompleteMultipartUpload rule, so parts of uploads that never finished are billed as storage. "
                      f"{len(stale)} unfinished upload(s) older than 7 days found now; the oldest started {max(stale)} days ago.")
        elif b["uploads_readable"]:
            detail = "No AbortIncompleteMultipartUpload rule. No stale uploads today, so this costs nothing yet; the rule stops the next one."
        else:
            detail = "No AbortIncompleteMultipartUpload rule, and the role could not list multipart uploads, so none were counted."
        findings.append(("uploads" if stale or not b["uploads_readable"] else "uploads-hygiene",
                         "Unfinished uploads are never cleaned up", detail, UPLOAD_RULE))

    standard = sum(v for k, v in b["sizes"].items() if k in STANDARD_TYPES)
    has_transition = any(r.get("Transitions") for r in enabled)
    if gb(standard) >= threshold_gb and not has_transition:
        ceiling = gb(standard) * (PRICE_STANDARD_GB - PRICE_IA_GB) - b["objects"] / 1000 * PRICE_MONITORING_PER_1000
        findings.append(("tiering", "Everything pays the Standard rate",
                         f"{gb(standard):,.1f} GB sits in S3 Standard with no lifecycle transition. If all of it turned out "
                         f"to be cold, Intelligent-Tiering would save at most ${max(ceiling, 0):,.2f}/month after its "
                         f"monitoring fee ({b['objects']:,.0f} objects). Objects under 128 KB are not tiered and should "
                         "stay as they are. The real figure depends on how often the files are read.",
                         {"ID": "tier-cold-files", "Status": "Enabled", "Filter": {"ObjectSizeGreaterThan": 131072},
                          "Transitions": [{"Days": 0, "StorageClass": "INTELLIGENT_TIERING"}]}))
    return findings


def s3_bill(profile):
    end = dt.date.today().replace(day=1)
    start = (end - dt.timedelta(days=1)).replace(day=1)
    start = (start - dt.timedelta(days=1)).replace(day=1)
    start = (start - dt.timedelta(days=1)).replace(day=1)  # three full months
    data = aws(["ce", "get-cost-and-usage", "--time-period", f"Start={start},End={end}", "--granularity", "MONTHLY",
                "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=USAGE_TYPE",
                "--filter", json.dumps({"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Simple Storage Service"]}})],
               profile, "us-east-1", ignore=("AccessDenied", "not enabled", "UnrecognizedClient"))
    if data is None:
        return None, 0
    months = len(data["ResultsByTime"]) or 1
    lines = {}
    for month in data["ResultsByTime"]:
        for g in month["Groups"]:
            lines[g["Keys"][0]] = lines.get(g["Keys"][0], 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
    return {k: v / months for k, v in sorted(lines.items(), key=lambda kv: -kv[1])}, months


def report(account, buckets, bill, months, threshold_gb):
    out = [f"# S3 cost review for account {account}", "",
           f"Read-only, {dt.date.today()}. Prices are us-east-1 list prices; the bill below is what you actually pay.", "",
           "## Buckets", "", "| Bucket | Region | Versioning | Standard GB | Total GB | Objects | Findings |", "|---|---|---|---:|---:|---:|---|"]
    all_findings = []
    for b in buckets:
        f = assess(b, threshold_gb)
        all_findings.append((b, f))
        standard = sum(v for k, v in b["sizes"].items() if k in STANDARD_TYPES)
        total = sum(b["sizes"].values())
        out.append(f"| {b['name']} | {b['region']} | {b['versioning']} | {gb(standard):,.1f} | {gb(total):,.1f} | {b['objects']:,.0f} | {len(f)} |")
    out += ["", "## Findings", ""]
    if not any(f for _, f in all_findings):
        out.append("None. Every bucket expires old versions, aborts unfinished uploads, and either tiers its data or is under the size threshold.")
    hygiene = []
    for b, findings in all_findings:
        for key, title, detail, rule in findings:
            if key == "uploads-hygiene":
                hygiene.append(b["name"])
                continue
            out += [f"### {b['name']}: {title}", "", detail, ""]
            if rule:
                out += ["Proposed lifecycle rule (not applied; add it to the bucket's existing rules, never replace them):", "",
                        "```json", json.dumps(rule, indent=2), "```", ""]
    if hygiene:
        out += ["### Buckets with no abort rule and no stale uploads today", "",
                "Nothing is billed for this yet. Adding the rule below to each of them stops the next failed upload from sitting there:", ""]
        out += [f"- {name}" for name in hygiene]
        out += ["", "```json", json.dumps(UPLOAD_RULE, indent=2), "```", ""]
    out += ["## S3 bill by usage type", ""]
    if bill is None:
        out.append("Cost Explorer could not be read (no ce:GetCostAndUsage permission, or Cost Explorer is not enabled on this account).")
    else:
        out += [f"Monthly average over the last {months} full month(s).", "", "| Usage type | USD/month |", "|---|---:|"]
        out += [f"| {k} | {v:,.2f} |" for k, v in list(bill.items())[:15]]
        out += ["", f"Total: ${sum(bill.values()):,.2f}/month. TimedStorage lines are storage; Requests, DataTransfer and Retrieval lines are usage, which these three settings do not change."]
    out += ["", "## Not checked", "",
            "- How much of each bucket is old versions (needs S3 Inventory).",
            "- How often files are read (needs S3 Storage Lens advanced metrics or server access logs). The tiering figure above is a ceiling, not a forecast.",
            "- Requests, data transfer, replication and Glacier retrieval costs.", ""]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile")
    ap.add_argument("--buckets", help="comma-separated names; default is every bucket the role can list")
    ap.add_argument("--threshold-gb", type=float, default=10.0, help="Standard GB below which tiering is not suggested")
    args = ap.parse_args()
    account = aws(["sts", "get-caller-identity"], args.profile)["Account"]
    names = args.buckets.split(",") if args.buckets else [b["Name"] for b in aws(["s3api", "list-buckets"], args.profile)["Buckets"]]
    buckets = []
    for name in names:
        try:
            buckets.append(inspect(name, args.profile))
        except RuntimeError as e:
            print(f"skipping {name}: {e}", file=sys.stderr)
    bill, months = s3_bill(args.profile)
    print(report(account, buckets, bill, months, args.threshold_gb))


def test():
    base = {"name": "b", "region": "us-east-1", "versioning": "Enabled", "rules": [], "stale_uploads": [12, 40],
            "uploads_readable": True, "sizes": {"StandardStorage": 100 * 1024 ** 3}, "objects": 20000}
    keys = [f[0] for f in assess(base)]
    assert keys == ["versions", "uploads", "tiering"], keys
    assert "oldest started 40 days" in assess(base)[1][2]
    ceiling = 100 * (PRICE_STANDARD_GB - PRICE_IA_GB) - 20 * PRICE_MONITORING_PER_1000
    assert f"${ceiling:,.2f}/month" in assess(base)[2][2]

    fixed = dict(base, rules=[
        {"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 30}},
        {"Status": "Enabled", "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
        {"Status": "Enabled", "Transitions": [{"Days": 0, "StorageClass": "INTELLIGENT_TIERING"}]}])
    assert assess(fixed) == [], assess(fixed)

    disabled_rules = dict(base, rules=[{"Status": "Disabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}])
    assert "versions" in [f[0] for f in assess(disabled_rules)]

    small = dict(base, versioning="Suspended", sizes={"StandardStorage": 1024 ** 3}, stale_uploads=[], uploads_readable=False)
    found = assess(small)
    assert [f[0] for f in found] == ["uploads"], found
    assert "could not list" in found[0][2]

    quiet = dict(small, uploads_readable=True)
    assert [f[0] for f in assess(quiet)] == ["uploads-hygiene"]
    quiet_text = report("1", [quiet], None, 0, 10.0)
    assert "no stale uploads today" in quiet_text and "### b: Unfinished" not in quiet_text

    ia_only = dict(base, versioning="Disabled", sizes={"StandardIAStorage": 500 * 1024 ** 3}, rules=[
        {"Status": "Enabled", "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}])
    assert assess(ia_only) == []

    text = report("123456789012", [base], {"TimedStorage-ByteHrs": 12.5, "Requests-Tier1": 0.4}, 3, 10.0)
    assert "| b | us-east-1 | Enabled | 100.0 | 100.0 | 20,000 | 3 |" in text
    assert "Total: $12.90/month" in text
    assert "Cost Explorer could not be read" in report("1", [], None, 0, 10.0)
    print("review.py self-check ok")


if __name__ == "__main__":
    test() if sys.argv[1:] == ["test"] else main()
