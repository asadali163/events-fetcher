"""Upload a city's clean event list (output/<city>/public.json) to S3.

The object at upload.key is overwritten on every run (S3 replaces it
atomically: readers get either the old or the new file, never half of one).

Settings come from core/pipeline.yaml -> upload (bucket, region, key,
enabled). Credentials are never stored in this repo: boto3 reads them from the
environment (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, loaded from .env), from
~/.aws/credentials, or from the server's IAM role.

Behaviour:
  - upload disabled, or bucket/key not set   -> skipped (logged)
  - no AWS credentials found                 -> skipped with a WARNING, so the
                                                daily job still succeeds before
                                                keys are configured
  - file missing, not a JSON list, or empty  -> error (never overwrite good
                                                data in S3 with nothing)
  - S3 rejects the upload (wrong key, no
    permission, network...)                  -> error; the scheduler retries
                                                the daily job later

After the events upload, the city's dataset file (upload.dataset.key, e.g.
.../cities/paris/dataset.json) is downloaded and ONLY the date part of its
"asOf" value is set to the run date (city's timezone) + upload.dataset.offset_days
(default -1: a run on 23 Nov writes 22 Nov); the time part and every other
byte stay as they are. Unchanged -> not uploaded.
No upload.dataset.key -> skipped. Any problem (missing file, no "asOf",
upload refused) is an error, so the daily job is retried.

Then the CloudFront cache is invalidated
(upload.cloudfront.distribution_id + paths, default "/*") so readers get the
new file at once. No distribution_id -> skipped. An invalidation error fails
the job too (the retry re-uploads the same file and invalidates again).

Usage:
    python -m core.upload Paris                  # upload output/paris/public.json
    python -m core.upload Paris --file x.json    # upload another file
    python -m core.upload Paris --dry-run        # show what would be uploaded
    python -m core.upload Paris --invalidate-only   # only clear the CloudFront cache
    python -m core.upload Paris --dataset-only      # only set dataset.json asOf (+ invalidate)
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.pipeline_config import get_city_config  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output"


class UploadError(Exception):
    pass


def default_public_file(city):
    return OUTPUT_DIR / re.sub(r"[^a-z0-9]+", "_", city.lower()).strip("_") / "public.json"


def _read_payload(path):
    path = Path(path)
    if not path.exists():
        raise UploadError(f"{path} does not exist - run the export first")
    body = path.read_bytes()
    try:
        events = json.loads(body)
    except ValueError as error:
        raise UploadError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(events, list):
        raise UploadError(f"{path} must contain a JSON list of events")
    if not events:
        raise UploadError(f"{path} has 0 events - refusing to overwrite the file in S3 with an empty list")
    return body, len(events)


def _session(region=None):
    """boto3 session, or None when no AWS credentials are available."""
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    import boto3

    session = boto3.session.Session(region_name=region or None)
    return session if session.get_credentials() is not None else None


def _s3_client(region):
    session = _session(region)
    return session.client("s3") if session else None


def _cloudfront_client():
    session = _session()
    return session.client("cloudfront") if session else None


def invalidate_cloudfront(city, cfg=None):
    """Create a CloudFront invalidation. Returns a summary dict; raises on AWS errors."""
    cfg = cfg if cfg is not None else (get_city_config(city).get("upload") or {})
    cf = cfg.get("cloudfront") or {}
    distribution_id = cf.get("distribution_id")
    paths = [str(p) for p in (cf.get("paths") or ["/*"])]
    summary = {"distribution_id": distribution_id, "paths": paths, "status": None}
    if not distribution_id:
        summary["status"] = "skipped: upload.cloudfront.distribution_id not set"
        print(f"[upload] {city}: CloudFront {summary['status']}")
        return summary
    client = _cloudfront_client()
    if client is None:
        summary["status"] = "skipped: no AWS credentials"
        print(f"[upload] WARNING {city}: CloudFront {summary['status']}")
        return summary
    response = client.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": len(paths), "Items": paths},
            "CallerReference": f"events-{re.sub(r'[^a-z0-9]+', '-', city.lower())}-{time.time():.0f}",
        },
    )
    invalidation = response.get("Invalidation") or {}
    summary.update(status="created", id=invalidation.get("Id"), aws_status=invalidation.get("Status"))
    print(f"[upload] {city}: CloudFront invalidation {summary['id']} ({summary['aws_status']}) "
          f"on {distribution_id} for {', '.join(paths)}")
    return summary


_DATE_IN_VALUE_RE = r'("{field}"\s*:\s*")(\d{{4}}-\d{{2}}-\d{{2}})'


def set_as_of(text, today, field="asOf"):
    """Return text with only the YYYY-MM-DD part of the top-level `field`
    value replaced by `today`. Raises UploadError if that isn't possible
    without touching anything else."""
    try:
        before = json.loads(text)
    except ValueError as error:
        raise UploadError(f"dataset file is not valid JSON: {error}") from error
    if not isinstance(before, dict) or not isinstance(before.get(field), str):
        raise UploadError(f'dataset file has no top-level string "{field}"')
    pattern = re.compile(_DATE_IN_VALUE_RE.format(field=re.escape(field)))
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise UploadError(f'expected exactly one "{field}": "YYYY-MM-DD..." in the dataset file, found {len(matches)}')
    new_text = pattern.sub(lambda m: m.group(1) + today, text, count=1)
    after = json.loads(new_text)
    # Safety net: nothing but the date of `field` may differ.
    if {k: v for k, v in after.items() if k != field} != {k: v for k, v in before.items() if k != field} \
            or not after[field].startswith(today) or after[field][10:] != before[field][10:]:
        raise UploadError(f'refusing to upload: editing "{field}" would change more than its date')
    return new_text, before[field], after[field]


def update_dataset(city, cfg, client, today=None, dry_run=False):
    """Set the dataset file's asOf date to today. Returns a summary dict."""
    ds = cfg.get("dataset") or {}
    key, field = ds.get("key"), ds.get("field") or "asOf"
    summary = {"key": key, "status": None}
    if not key:
        summary["status"] = "skipped: upload.dataset.key not set"
        print(f"[upload] {city}: dataset {summary['status']}")
        return summary
    if today is None:
        tz = ZoneInfo(get_city_config(city).get("timezone", "UTC"))
        offset = int(ds.get("offset_days", -1))
        today = (datetime.now(tz).date() + timedelta(days=offset)).isoformat()

    obj = client.get_object(Bucket=cfg["bucket"], Key=key)
    raw = obj["Body"].read()
    text = raw.decode("utf-8")
    new_text, old_value, new_value = set_as_of(text, today, field)
    summary.update(old=old_value, new=new_value)
    if new_text == text:
        summary["status"] = "unchanged (already today)"
    elif dry_run:
        summary["status"] = "dry run"
    else:
        extra = {k: obj[k] for k in ("CacheControl", "ContentEncoding", "ContentLanguage", "ContentDisposition") if obj.get(k)}
        client.put_object(
            Bucket=cfg["bucket"], Key=key, Body=new_text.encode("utf-8"),
            ContentType=obj.get("ContentType") or "application/json",
            Metadata=obj.get("Metadata") or {}, **extra,
        )
        summary["status"] = "updated"
    print(f"[upload] {city}: dataset s3://{cfg['bucket']}/{key} {field} {old_value} -> {new_value}: {summary['status']}")
    return summary


def run_upload(city, path=None, dry_run=False):
    """Returns a summary dict. Raises UploadError / botocore errors when the
    upload should be retried."""
    cfg = get_city_config(city).get("upload") or {}
    bucket, key, region = cfg.get("bucket"), cfg.get("key"), cfg.get("region")
    path = Path(path) if path else default_public_file(city)
    summary = {"city": city, "file": str(path), "bucket": bucket, "key": key, "status": None}

    if not cfg.get("enabled"):
        summary["status"] = "skipped: upload disabled in pipeline.yaml"
    elif (cfg.get("provider") or "s3") != "s3":
        raise UploadError(f"unknown upload provider {cfg.get('provider')!r} (only 's3' is supported)")
    elif not bucket or not key:
        summary["status"] = "skipped: upload.bucket / upload.key not set in pipeline.yaml"
    if summary["status"]:
        print(f"[upload] {city}: {summary['status']}")
        return summary

    body, count = _read_payload(path)
    summary.update(events=count, bytes=len(body))
    target = f"s3://{bucket}/{key}"
    if dry_run:
        summary["status"] = "dry run"
        print(f"[upload] {city}: would upload {count} events ({len(body):,} bytes) to {target}")
        return summary

    client = _s3_client(region)
    if client is None:
        summary["status"] = "skipped: no AWS credentials (set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env)"
        print(f"[upload] WARNING {city}: {summary['status']}")
        return summary

    response = client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache",
    )
    summary.update(status="uploaded", etag=str(response.get("ETag", "")).strip('"'))
    print(f"[upload] {city}: uploaded {count} events ({len(body):,} bytes) to {target}")
    summary["dataset"] = update_dataset(city, cfg, client)
    summary["cloudfront"] = invalidate_cloudfront(city, cfg)  # one invalidation covers both files
    return summary


def run_dataset_only(city, dry_run=False):
    cfg = get_city_config(city).get("upload") or {}
    client = _s3_client(cfg.get("region"))
    if client is None:
        raise UploadError("no AWS credentials")
    summary = update_dataset(city, cfg, client, dry_run=dry_run)
    if summary["status"] == "updated":
        summary["cloudfront"] = invalidate_cloudfront(city, cfg)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Upload a city's event list to S3.")
    parser.add_argument("city")
    parser.add_argument("--file", default=None, help="file to upload (default: output/<city>/public.json)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--invalidate-only", action="store_true", help="only clear the CloudFront cache")
    parser.add_argument("--dataset-only", action="store_true", help="only set dataset.json asOf (run date + offset_days)")
    args = parser.parse_args()
    try:
        if args.dataset_only:
            run_dataset_only(args.city, args.dry_run)
        elif args.invalidate_only:
            invalidate_cloudfront(args.city)
        else:
            run_upload(args.city, args.file, args.dry_run)
    except Exception as error:
        print(f"[upload] FAILED: {type(error).__name__}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
